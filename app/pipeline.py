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
    own = (get_setting(s, f"model_{kind}", "") or "").strip()      # Admin: a model chosen for this one shot
    if own.startswith("openrouter:") and not get_setting(s, "use_openrouter", False):
        own = ""                                                   # OpenRouter is switched off: Gemini only
    if own:
        return (own if own.startswith("openrouter:") else config.model_name(own)), size
    pick = hero_m if kind in ("hero", "detail") else other_m
    # the model boxes in Admin -> API & models are what "pro" and "fast" mean (blank = built-in default)
    chosen = {"pro": get_setting(s, "model_hero_detail", ""), "fast": get_setting(s, "model_other", "")}.get(pick) or ""
    return config.model_name(chosen.strip() or config.mode_model(pick)), size


def shot_card(s: Session, p: Product, kind: str) -> dict:
    """The brand's settings for this shot (method, pose, scene, props, extra prompt), if the brand has any."""
    row = library(s, "brands").get(p.brand_id) or {}
    return ((row.get("shots") or {}).get(kind) or {}) if (row.get("shots") or {}).get(kind, {}).get("mode") == "custom" else {}


def _render_accurate(s, p, sh, spec, prov, model, size, refs, style, angle_raw, facts, correction, fallback_raw,
                     card: dict | None = None):
    """One shot with the accuracy engine. Returns (final jpeg, how)."""
    from . import engine
    card = card if card is not None else shot_card(s, p, sh.kind)
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


def _j_list(v) -> str:
    return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v or "")


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
        plan = None
        kept, plan_refs = [], {}
        if (p.data or {}).get("lib_key"):
            from . import library as lib
            key = p.data["lib_key"]
            lp = s.scalar(select(lib.LibProduct).where(lib.LibProduct.key == key))
            plan = None
            if lp is not None and get_setting(s, "planner_on", True) and not accurate:
                from . import planner
                rec = (lp.data or {}).get("plan")
                if not rec or rec.get("stale") or rec.get("status") != "ok":
                    rec = planner.make_plan(s, key, prov, charge=lambda used: _charge(
                        s, p, "plan", used, config.COST_PLANNER))  # one call, best model; errors stop this product
                if rec.get("status") != "ok":
                    p.stage, p.error = "error", "Every photo of this product was discarded by the analysis. Check the Library."
                    s.commit()
                    return
                if planner.needs_review(s, rec):
                    p.stage = "plan"
                    p.error = "Shot plan needs your approval in the Library: " + ((rec.get("json") or {}).get("review_reason") or "flagged by the analysis")
                    p.data = {k: v for k, v in (p.data or {}).items() if k not in ("run_now", "only_shots")}
                    s.commit()
                    return
                plan = rec["json"]
                static = planner.static_rules(s)
            if lp is not None and plan is None and not ((lp.data or {}).get("memory") or {}).get("facts"):
                try:                                       # study once (≈ ₹0.3), reused for every future shoot
                    lib.study_product(s, key, prov)
                    _charge(s, p, "memory", config.model_name(config.MODEL_VISION), config.COST_VISION * 2)
                except Exception as e:
                    print(f"[pipeline] product study failed for {p.code}: {e}")
            if lp is not None:
                p.data = {**(p.data or {}), "memory": (lp.data or {}).get("memory") or {}}
            s.commit()
            for r in lib.refs_with_angles(s, key, 6):
                angle_raw.setdefault(r["angle"] or "unknown", r["path"])
            rank = {"front": 0, "unknown": 1, "": 1, "angle": 2, "side": 3}     # whole-product views first
            _src = {i.path: i.source for i in s.scalars(select(lib.LibImage).where(
                lib.LibImage.key == key, lib.LibImage.source.in_(("store", "website"))))}
            kept = [r["path"] for r in sorted(lib.refs_with_angles(s, key, 6),
                                              key=lambda r: ({"store": 0, "website": 1}.get(_src.get(r["path"]), 2),
                                                             rank.get(r["angle"] or "", 5)))]
            if plan is not None:
                rec = (lp.data or {}).get("plan") or {}
                ids = rec.get("photos") or []
                def _rel(ix):
                    try:
                        img = s.get(lib.LibImage, ids[int(ix)])
                    except (ValueError, TypeError, IndexError):
                        return None
                    return img.path if img is not None and img.path in kept else None
                best = _rel(plan.get("best_reference"))
                if best:
                    kept = [best] + [k for k in kept if k != best]
                plan_refs = {k: _rel(((plan.get("shots") or {}).get(k) or {}).get("reference_photo")) for k in SHOT_NAMES.values()}
            prompts = {x["no"]: x for x in build_prompts(s, p, p.analysis or {}, rv, notes)}   # now with product facts
        refs = []
        ref_paths = ((p.data or {}).get("refs") or [])[1:]
        if angle_raw:                                     # only verified (not discarded) photos of this exact code
            ref_paths = list(dict.fromkeys(angle_raw.values()))[1:]
        for rel in ref_paths:   # extra angles of the same product from the Library
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
            main_rel = p.raw_path
            if kept:                                          # verified photos of this exact code only
                main_rel = plan_refs.get(sh.kind) or kept[0]
                try:
                    shot_raw = storage.read(main_rel)
                except Exception:
                    shot_raw, main_rel = raw, p.raw_path
                extra_rels = [k for k in kept if k != main_rel]
                refs = []
                for rel in extra_rels:
                    try:
                        refs.append(storage.read(rel))
                    except Exception:
                        pass
                    if len(refs) >= 2:
                        break
                sent_rels = [main_rel] + extra_rels[:len(refs)]
            else:
                sent_rels = [main_rel] + list(((p.data or {}).get("refs") or [])[1:3])
            facts = ""
            mem = (p.data or {}).get("memory") or {}
            if mem.get("facts"):
                from .dataset import memory_block
                facts = memory_block(mem).split("\n", 1)[-1].replace("\n", "; ")
            hero_ref = None
            h = shots.get(1)
            if (no != 1 and not accurate and get_setting(s, "hero_review", True) and h is not None and h.image_path
                    and h.status != "approved"):
                sh.status = "wait"
                s.commit()
                stop_reason = "Approve the hero first: shots 2-4 copy it, so a wrong hero would spoil all of them"
                break
            if no != 1 and h is not None and h.image_path and h.status in ("pass", "approved", "unchecked"):
                try:
                    hero_ref = storage.read(h.image_path)
                except Exception:
                    hero_ref = None
            if plan is not None:
                mem_ok = (p.data or {}).get("memory") or {}
                approved = ""
                if mem_ok.get("approved") and mem_ok.get("facts"):
                    from .dataset import memory_block
                    approved = memory_block(mem_ok).replace("[PRODUCT FACTS: reproduce exactly, these override anything else]",
                                                            "[PRODUCT FACTS approved by a person: these override anything else]")
                spec = {**spec, "prompt": planner.render(plan, sh.kind, static, anchored=bool(hero_ref), approved_facts=approved,
                                                         look=planner.house_look(s))
                        + (f"\n\nREVIEWER ADJUSTMENT (highest priority, never change the product): {spec['fill'].get('reviewer_note')}"
                           if (spec.get("fill") or {}).get("reviewer_note") else "")}
                facts = planner.qc_facts(plan) + ("; " + facts if facts else "")
                prompt = spec["prompt"]
                call_refs = ([hero_ref] if hero_ref else []) + refs[:2]
            card = {} if plan is not None else shot_card(s, p, sh.kind)
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
            if plan is None and (cl or ang):
                spec = {**spec, "prompt": spec["prompt"] + "\n\n[BRAND SHOT CARD - follow this over the generic scene "
                        "above]\n" + "\n".join(cl + ([ang] if ang else []))}
            if plan is None:
              spec = {**spec, "prompt": spec["prompt"] + (f"\n\n[BRAND LOOK - light, colours and mood only, copy no "
                                                         f"objects]\n{style_note}" if style_note else "")
                    + LOCK + ((ANCHOR.replace("Keep the same face of the product towards the camera and the same orientation; you may move the camera higher, closer or farther, but do not rotate the product to reveal unseen sides.", "Show the product at the angle of the FIRST image (a real photo of that side).") if used_photo else ANCHOR) if hero_ref else "")}
            override = (((p.data or {}).get("prompt_override") or {}).get(str(no)) or "").strip()
            if override:
                note_txt = ((spec.get("fill") or {}).get("reviewer_note") or "").strip()
                spec = {**spec, "prompt": override + (f"\n\nREVIEWER ADJUSTMENT (highest priority, never change the "
                                                      f"product): {note_txt}" if note_txt else "")}
            prompt = spec["prompt"]
            call_refs = ([hero_ref] if hero_ref else []) + refs[:2]
            sh.prompt = prompt
            sent = {"photos": sent_rels, "hero": bool(hero_ref), "override": bool(override), "plan": plan is not None}
            result = None
            escalate = (get_setting(s, "escalate_model", "") or "").strip()
            if escalate.startswith("openrouter:") and not get_setting(s, "use_openrouter", False):
                escalate = ""
            escalated = False
            for attempt in range(retries + 1 + (1 if escalate and escalate != model else 0)):
                if STOP.is_set():
                    stop_reason = "Stopped by you"
                    break
                est = config.image_cost(model, size) + config.COST_VISION
                if p.cost + est > cap:
                    stop_reason = f"Stopped at the ${cap:.2f} per-product cost limit"
                    break
                sh.attempts = (sh.attempts or 0) + 1
                how = {"method": "fresh"}
                real_px = False
                if (not accurate and no == 1 and get_setting(s, "engine_mode", "classic") == "hybrid" and kept
                        and not override):
                    from . import engine as _eng
                    try:                                  # clean, high-res packshot: keep the real product pixels
                        from PIL import Image as _PI
                        import io as _io2
                        _im = _PI.open(_io2.BytesIO(shot_raw))
                        real_px = max(_im.size) >= 1200 and _eng.place(shot_raw, "hero")["ok"]
                    except Exception:
                        real_px = False
                if accurate or real_px:
                    card_acc = None
                    if real_px and plan is not None:
                        ps = (plan.get("shots") or {}).get("hero") or {}
                        card_acc = {"mode": "custom", "method": "edit", "scene": ps.get("scene", ""),
                                    "background": ps.get("surface", ""), "props": _j_list(ps.get("props")),
                                    "extra": ps.get("light", "")}
                    img, how = _render_accurate(s, p, sh, spec, prov, model, size, refs, style,
                                                {"front": main_rel} if real_px else angle_raw, facts,
                                                prompt if attempt else "", shot_raw, card=card_acc)
                else:
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
                if how.get("exact"):                     # real product pixels: shape and text cannot have drifted
                    result = {"checks": {"product_exact": True}, "reasons": {}, "how": how}
                else:
                    try:
                        qref = _sheet([hero_ref, shot_raw] if hero_ref else [shot_raw] + refs[:2])
                        result = prov.qc(qref, img, {"kind": sh.kind, "no": no, "facts": facts, "sheet": True,
                                         "vs_hero": bool(hero_ref)}, rv.data)
                        _charge(s, p, "qc", config.model_name(config.MODEL_VISION), config.COST_VISION, sh)
                    except Exception as e:               # keep the image; a person checks it instead
                        print(f"[pipeline] QC failed for {p.code} shot {no}: {e}")
                        result = {"checks": {}, "reasons": {"qc": f"Automatic QC unavailable: {str(e)[:200]}"},
                                  "unchecked": True}
                    result["how"] = how
                result["sent"] = {**sent, "model": sh.model or model, "size": size}
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
