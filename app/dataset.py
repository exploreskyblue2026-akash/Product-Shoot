"""Static + dynamic dataset: seeding, lookups and the 6-layer prompt builder."""
import csv
import json
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import DATA_DIR, MODEL_IMAGE_PRO, MODEL_IMAGE_FAST
from .db import RuleVersion, LibraryRow, Setting, User, get_setting, set_setting

LIB_TABLES = {"brands": "brand_id", "categories": "category_id", "moods": "mood_id", "props": "prop_id"}

DEFAULT_STRENGTH = {"lighting": "strict", "attention": "normal", "size": "normal",
                    "props": "normal", "negative_space": "relaxed"}

DEFAULT_SETTINGS = {
    "pipeline_mode": "v1",         # v1: AI draws product + scene · v2: real cut-out + AI scene, shoot sheet only
    "scene_strategy": "auto",      # v2 scenes: auto | mix | pool | fresh
    "scene_reuse": 10,             # v2: a pool scene is reused for at most this many products
    "fresh_brands": "",            # v2: brands that always get fresh scenes (comma separated)
    "autopilot": False,
    "auto_approve": True,          # approve when every QC check passes
    "retries": 0,                  # automatic regenerations after a QC fail (each one costs a full image)
    "cost_mode": "economy",        # economy | standard | premium  (model + resolution per shot)
    "default_shots": [1],          # shots generated automatically; others on demand from the studio
    "max_cost_product": 0.30,      # USD hard cap per product (images + checks)
    "daily_budget": 3.0,           # USD per day; autopilot pauses when reached
    "hero_gate": True,             # if the hero fails QC, don't generate the other shots
    "qc_mode": "light",            # light: only shape / label / artefacts can fail a shot; full: all 8 checks
    "usd_inr": 88.0,
    "publish_shopify": False,
    "budget_month": 150.0,
    "budget_action": "pause",      # pause | alert
    "model_hero_detail": MODEL_IMAGE_PRO,
    "model_other": MODEL_IMAGE_FAST,
    "file_naming": "{code}_{colour}_{no}-{shot}.jpg",
    "min_raw_px": 800,
}


def split(v):
    return [x.strip() for x in str(v or "").split("|") if x.strip()]


def _read_json(name):
    return json.loads((DATA_DIR / "static" / name).read_text(encoding="utf-8"))


def seed(s: Session, owner_email: str, owner_hash: str):
    """Idempotent first-run seed from the repo's data/ folder."""
    if not s.scalars(select(RuleVersion)).first():
        s.add(RuleVersion(version=1, status="live", created_by="seed", data={
            "studio_standard": _read_json("studio_standard.json"),
            "shot_directions": _read_json("shot_directions.json"),
            "qc_rules": _read_json("qc_rules.json"),
            "vision_schema": _read_json("vision_analysis_schema.json"),
            "prompt_template": (DATA_DIR / "static" / "prompt_template.txt").read_text(encoding="utf-8"),
            "strength": DEFAULT_STRENGTH,
        }))
    if not s.scalars(select(LibraryRow)).first():
        for table, keycol in LIB_TABLES.items():
            fname = "props_library.csv" if table == "props" else f"{table}.csv"
            with open(DATA_DIR / "dynamic" / fname, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    s.add(LibraryRow(table=table, key=row[keycol], data=row))
    for k, v in DEFAULT_SETTINGS.items():
        if s.get(Setting, k) is None:
            set_setting(s, k, v)
    if not s.scalars(select(User).where(User.email == owner_email)).first():
        s.add(User(email=owner_email, role="owner", password_hash=owner_hash, active=True))
    s.commit()


def live_rules(s: Session) -> RuleVersion:
    return s.scalars(select(RuleVersion).where(RuleVersion.status == "live").order_by(RuleVersion.version.desc())).first()


def library(s: Session, table: str) -> dict:
    return {r.key: r.data for r in s.scalars(select(LibraryRow).where(LibraryRow.table == table))}


def brand_from_text(s: Session, text: str) -> str:
    t = (text or "").lower()
    first = t.split()[0] if t.split() else ""
    for key, b in library(s, "brands").items():
        if key.startswith("_"):
            continue
        if any(first.upper().startswith(pfx + "-") or first.upper().startswith(pfx + "_") for pfx in split(b.get("code_prefixes"))):
            return key
        if any(k and k in t for k in split(b.get("detect_keywords"))):
            return key
    return "_default"


# ---------------------------------------------------------------- prompt builder
SHOT_MODEL = {"hero": "model_hero_detail", "detail": "model_hero_detail"}

LIGHT_TEXT = {
    "strict": "STRICTLY SOFT, diffused light from a large soft source at upper-left (~45°), white fill opposite, soft feathered contact shadow. No hard light, no direct sun, no glare.",
    "normal": "Soft diffused light from upper-left, gentle fill, soft contact shadow. Avoid hard light and glare.",
    "relaxed": "Soft natural light; gentle shadows.",
}
ATTN_TEXT = {"strict": "Exactly ~70% of visual attention on the product, ~30% on set and props.",
             "normal": "About 70% of visual attention on the product, 30% on set and props.",
             "relaxed": "Product is the clear focal point; props support it."}


def pick_scene(s: Session, product, analysis: dict) -> dict:
    """Resolve mood/surface/props. Vision analysis wins; dataset rules fill gaps."""
    brands, cats, moods, props = (library(s, t) for t in LIB_TABLES)
    brand = brands.get(product.brand_id) or brands.get("_default", {})
    cat = cats.get(product.category_id) or next(iter(cats.values()))
    mood_id = (analysis.get("mood_id") or (product.data or {}).get("mood_override")
               or brand.get("mood_override") or (split(cat.get("default_moods")) or ["minimal_clean"])[0])
    mood = moods.get(mood_id) or next(iter(moods.values()))
    surfaces = split(cat.get("surfaces"))
    preferred = [p.split()[-1] for p in split(mood.get("surfaces_preferred", "").replace(", ", "|"))]
    surface = analysis.get("surface") or next((x for x in surfaces if any(p in x for p in preferred)), surfaces[0] if surfaces else "plain surface")
    donts = (brand.get("brand_donts") or "").lower()

    def props_for(shot, n):
        chosen = [props[p]["prop"] for p in (analysis.get("props") or {}).get(shot, []) if p in props]
        if not chosen:
            for p in props.values():
                if cat.get("category_id") in split(p["categories"]) and (shot in split(p["shots"]) or "any" in split(p["shots"])) \
                        and mood_id in split(p["moods"]) and not any(w in p["prop"].lower() for w in ("plastic",) if w in donts):
                    chosen.append(p["prop"])
            if len(chosen) < n and shot == "use_case":
                chosen += [x for x in props_for("lifestyle", 5) if x not in chosen]
        chosen = chosen[:n]
        if chosen and len(chosen) % 2 == 0:
            chosen = chosen[:-1]
        return chosen

    human = analysis.get("human_mode") or brand.get("human_allowed") or cat.get("human_mode") or "none"
    detail_mode = analysis.get("detail_mode") or ("macro" if cat.get("material_is_visual") == "true" else "feature_callout")
    return {"brand": brand, "cat": cat, "mood": mood, "mood_id": mood_id, "surface": surface, "human": human,
            "detail_mode": detail_mode,
            "props": {"use_case": props_for("use_case", 3), "arrangement": props_for("arrangement", 3),
                      "lifestyle": props_for("lifestyle", 5), "detail": props_for("detail", 1)}}


def build_prompts(s: Session, product, analysis: dict, rv: RuleVersion, notes: dict | None = None) -> list[dict]:
    rules = rv.data
    std, shots, tpl = rules["studio_standard"], rules["shot_directions"]["shots"], rules["prompt_template"]
    st = {**DEFAULT_STRENGTH, **rules.get("strength", {})}
    sc = pick_scene(s, product, analysis)
    d = product.data or {}
    out = []
    for sh in shots:
        sid = sh["id"]
        if sid == "detail":
            sub = sh[sc["detail_mode"]]
            camera = sub["camera"].replace("{hero_backdrop}", sc["brand"].get("hero_backdrop", "a plain backdrop"))
            framing = sub["framing"].replace("{detail_focus}", sc["cat"].get("detail_focus", "material"))
            dof = "very shallow" if sc["detail_mode"] == "macro" else "deep"
            scene = sub["set"].replace("{hero_backdrop}", sc["brand"].get("hero_backdrop", "a plain backdrop"))
            plist = sc["props"]["detail"] if sc["detail_mode"] == "macro" else []
        else:
            camera, framing, dof = sh["camera"], sh["framing"], sh["depth_of_field"]
            scene = {"hero": "clean seamless backdrop", "use_case": sc["cat"].get("use_scene", "the product in use"),
                     "arrangement": sc["cat"].get("arrangement_rule", "flat-lay arrangement"),
                     "lifestyle": sc["cat"].get("lifestyle_scene", "styled lifestyle scene")}[sid]
            plist = [] if sid == "hero" else sc["props"].get(sid, [])
        surface = sc["brand"].get("hero_backdrop", "warm off-white #F4F1EC") if sid == "hero" else sc["surface"]
        human = "No people." if sid != "use_case" else {
            "hands": "Show only natural, well-groomed hands using the product; no face.",
            "partial_person": "Show a partial person using the product; face out of frame.",
            "hands_or_result": "Show hands at work OR the finished result of use.",
        }.get(sc["human"], "No people; show the product in its place of use.")
        visible = ", ".join(analysis.get("visible_text") or []) or product.name
        fill = {
            "product_description": analysis.get("product_description") or f"{product.name} — {d.get('contents', '')}, {d.get('material', '')}",
            "product_colours": ", ".join(analysis.get("dominant_colours_hex") or []) or "as in reference",
            "visible_text": visible, "item_count": analysis.get("item_count") or d.get("contents", "as in reference"),
            "shot_no": sh["no"], "shot_id": sid, "shot_purpose": sh["purpose"], "shot_camera": camera,
            "shot_framing": framing, "shot_dof": dof, "mood_name": sc["mood"].get("name", ""),
            "mood_palette": sc["mood"].get("palette", ""), "mood_styling": sc["mood"].get("styling_notes", ""),
            "scene": scene, "surface": surface, "light_ratio": sc["mood"].get("light_ratio", "2:1"),
            "human_line": human, "prop_count": len(plist), "props": ", ".join(plist) if plist else "none",
            "brand_name": sc["brand"].get("brand_name", "the brand"), "brand_positioning": sc["brand"].get("positioning", ""),
            "brand_palette": sc["brand"].get("palette_hex", ""), "brand_dos": sc["brand"].get("brand_dos") or "-",
            "brand_donts": sc["brand"].get("brand_donts") or "-",
            "negatives": ", ".join(std["global_negative"] + split(sc["mood"].get("avoid", "").replace(", ", "|"))),
        }
        p = tpl
        for k, v in fill.items():
            p = p.replace("{" + k + "}", str(v))
        # strength layer: rewrite the lighting + attention sentences
        p = p.replace(p.split("\n")[1], "Professional e-commerce product photograph, 4:5 vertical, 2000x2500 px. " + LIGHT_TEXT[st["lighting"]], 1)
        p = p.replace("The product is the clear hero: ~70% of visual attention on the product, ~30% on set and props.",
                      "The product is the clear hero. " + ATTN_TEXT[st["attention"]])
        if notes and notes.get(sh["no"]):
            p += f"\n\n[ADJUSTMENT FROM REVIEWER]\n{notes[sh['no']]}"
        out.append({"no": sh["no"], "kind": sid, "prompt": p, "fill": fill, "light": LIGHT_TEXT[st["lighting"]],
                    "model_setting": SHOT_MODEL.get(sid, "model_other")})
    return out
