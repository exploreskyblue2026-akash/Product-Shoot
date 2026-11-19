"""Static + dynamic dataset: seeding, lookups and the 6-layer prompt builder."""
import csv
import json
import os
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import DATA_DIR, MODEL_IMAGE_PRO, MODEL_IMAGE_FAST
from .db import RuleVersion, LibraryRow, Setting, User, get_setting, set_setting

LIB_TABLES = {"brands": "brand_id", "categories": "category_id"}


def or_gate(s: Session, raw: str) -> str:
    """An 'openrouter:...' model string counts only when the OpenRouter switch in Admin is on; otherwise it's
    treated as unset (falls back to the built-in Gemini default) so a stale box never silently calls OpenRouter."""
    raw = (raw or "").strip()
    return "" if raw.startswith("openrouter:") and not get_setting(s, "use_openrouter", False) else raw

DEFAULT_STRENGTH = {"lighting": "strict", "attention": "normal", "size": "normal",
                    "props": "normal", "negative_space": "relaxed"}

# Hero camera angle, decided from the product's real physical form (checklist B11) — never from the angle the
# reference photo happens to be shot at. Keyed by the vision analysis's "form" field.
FORM_CAMERA = {
    "flat_thin": "straight top-down flat lay, camera parallel to the surface",
    "long_thin": "top-down, or a high angle 60-75 degrees above",
    "standing": "eye level, or a low 3/4 angle",
    "boxy": "3/4 angle at 30-45 degrees above",
    "soft_flexible": "eye level, the product slightly slouched/resting naturally, not propped stiff",
    "reflective": "an angle chosen to avoid the studio/room reflecting in the surface — usually a slight 3/4 turn",
    "_default": "3/4 angle at 30-45 degrees above (the safest default when the form is unclear)",
}

DEFAULT_SETTINGS = {
    "engine_mode": "classic",      # accurate: keep the real photo (edit / crop / restore); classic: always regenerate
    "library_focus": False,        # False: scan everything in Raw input; True: only shoot-sheet / brand-queue codes
    "library_autoscan": True,      # re-scan the saved Drive link every 30 min (new files only)
    "library_cutouts": False,      # False: keep original photos (AI generation). True: cut-outs + composite engine
    "pipeline_mode": "v2",         # v2 = shoot-sheet flow (the only flow on screen); v1 = old 'add input' flow. v1: AI draws product + scene · v2: real cut-out + AI scene, shoot sheet only
    "scene_strategy": "auto",      # v2 scenes: auto | mix | pool | fresh
    "scene_reuse": 10,             # v2: a pool scene is reused for at most this many products
    "fresh_brands": "",            # v2: brands that always get fresh scenes (comma separated)
    "autopilot": False,
    "auto_approve": True,          # approve when every QC check passes
    "shopify_replace_images": True,  # automatic push: remove the product's old photos, keep only the new generated set
    "retries": 0,                  # automatic regenerations after a QC fail (each one costs a full image)
    "cost_mode": "economy",        # economy | standard | premium  (model + resolution per shot)
    "default_shots": [1, 2, 3, 4],          # shots generated automatically; others on demand from the studio
    "max_cost_product": 0.30,      # USD hard cap per product (images + checks)
    "daily_budget": 3.0,           # USD per day; autopilot pauses when reached
    "hero_gate": True,             # if the hero fails QC, don't generate the other shots
    "hero_review": True,           # shots 2-4 wait until a person approves the hero (they copy it)
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


QUALITY_PRESET = {
    "cost_mode": "max",            # Pro image model for all 4 shots, 2K
    "engine_mode": "classic",
    "default_shots": [1],          # hero first; the rest after you approve it
    "hero_gate": True,
    "qc_mode": "full",
    "retries": 0,
    "auto_approve": False,         # always review until quality is proven
    "publish_shopify": False,
    "max_cost_product": 1.0,
    "daily_budget": 10.0,
    "min_raw_px": 1500,
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
    # library rows: add any rows that are new in the shipped CSVs; never overwrite rows you edited in the tool
    have = {(r.table, r.key): r for r in s.scalars(select(LibraryRow))}
    for table, keycol in LIB_TABLES.items():
        fname = "props_library.csv" if table == "props" else f"{table}.csv"
        with open(DATA_DIR / "dynamic" / fname, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cur = have.get((table, row[keycol]))
                if cur is None:
                    s.add(LibraryRow(table=table, key=row[keycol], data=row))
                elif any(k not in (cur.data or {}) for k in row):          # new fields only; your edits stay
                    cur.data = {**row, **(cur.data or {})}
    # upgrade: 4-shot directions + website size in every rule version (your other rule edits are kept)
    new_dirs = _read_json("shot_directions.json")
    for rv in s.scalars(select(RuleVersion)):
        d = dict(rv.data or {})
        ids = [x.get("id") for x in (d.get("shot_directions") or {}).get("shots", [])]
        if ids and ids != [x["id"] for x in new_dirs["shots"]]:
            d["shot_directions"] = new_dirs
            d["prompt_template"] = (d.get("prompt_template") or "").replace("2000x2500", "1200x1500")
            rv.data = d
    for k, v in DEFAULT_SETTINGS.items():
        if s.get(Setting, k) is None:
            set_setting(s, k, v)
    # one-time switch to the agreed quality setup (Oct 2026); later edits in Admin are kept
    if not get_setting(s, "preset_quality_v1", False) and os.environ.get("SKIP_PRESETS") != "1":
        for k, v in QUALITY_PRESET.items():
            set_setting(s, k, v)
        set_setting(s, "preset_quality_v1", True)
    # Oct 30: back to full generation from the real photo (what produced the first good results)
    if not get_setting(s, "preset_engine_v2", False) and os.environ.get("SKIP_PRESETS") != "1":
        set_setting(s, "engine_mode", "classic")
        set_setting(s, "preset_engine_v2", True)
    if not get_setting(s, "rules_v3", False) and os.environ.get("SKIP_PRESETS") != "1":
        live = live_rules(s)
        if live is not None:                          # new live version; the old one stays under Versions
            d = dict(live.data or {})
            d["shot_directions"] = _read_json("shot_directions.json")
            d["prompt_template"] = (DATA_DIR / "static" / "prompt_template.txt").read_text(encoding="utf-8")
            top = max(v.version for v in s.scalars(select(RuleVersion)))
            live.status = "archived"
            s.add(RuleVersion(version=top + 1, status="live", created_by="Oct 30 shot rules", data=d))
        for k, v in {"cost_mode": "max", "engine_mode": "classic"}.items():
            set_setting(s, k, v)
        set_setting(s, "rules_v3", True)
    if not get_setting(s, "preset_engine_v3", False) and os.environ.get("SKIP_PRESETS") != "1":
        set_setting(s, "engine_mode", "hybrid")          # Nov 2: real product pixels for the hero when possible
        set_setting(s, "preset_engine_v3", True)
    if not get_setting(s, "preset_gemini_only_v1", False) and os.environ.get("SKIP_PRESETS") != "1":
        set_setting(s, "use_openrouter", False)            # decided: Gemini only
        for k in ("model_hero", "model_detail", "model_use_case", "model_lifestyle", "escalate_model"):
            if str(get_setting(s, k, "") or "").startswith("openrouter:"):
                set_setting(s, k, "")
        set_setting(s, "preset_gemini_only_v1", True)
    if not get_setting(s, "reset_troika_v1", False) and os.environ.get("SKIP_PRESETS") != "1":
        try:                                               # decided: start Troika clean (master rows kept)
            from .library import reset_brand
            reset_brand(s, "troika")
        except Exception as e:
            print(f"[seed] Troika reset skipped: {e}")
        set_setting(s, "reset_troika_v1", True)
    if not get_setting(s, "preset_fix_stale_gpt5_v1", False) and os.environ.get("SKIP_PRESETS") != "1":
        # "openai/gpt-5" was retired from OpenRouter (the lineup moved on to gpt-6.x) and 404s now. Any box
        # still pointing at it is corrected once: the escalate/retry box is a safety net and should never be
        # an OpenRouter model at all (clear it so a failed main model doesn't fail again on "retry"); the
        # analysis box gets a current, confirmed-vision OpenRouter model instead of being left broken.
        if (get_setting(s, "escalate_model", "") or "").startswith("openrouter:openai/gpt-5"):
            set_setting(s, "escalate_model", "")
        if (get_setting(s, "model_planner", "") or "").startswith("openrouter:openai/gpt-5"):
            set_setting(s, "model_planner", "openrouter:anthropic/claude-sonnet-5.5")
        set_setting(s, "preset_fix_stale_gpt5_v1", True)
    if not get_setting(s, "purge_prices_v1", False):        # commercial data (cost / price / stock) is never kept
        from .db import LibProduct
        for lp in s.scalars(select(LibProduct)):
            if (lp.data or {}).get("prices"):
                lp.data = {k: v for k, v in lp.data.items() if k != "prices"}
        set_setting(s, "purge_prices_v1", True)
    if not get_setting(s, "recheck_matches_v1", False) and os.environ.get("SKIP_PRESETS") != "1":
        from .library import recheck_matches
        try:
            recheck_matches(s)
        except Exception as e:
            print(f"[seed] match re-check failed: {e}")
        set_setting(s, "recheck_matches_v1", True)
    if not get_setting(s, "classic_camera_fix_v1", False):
        # 13 Nov 2026: the "classic" path (products with no lib_key, uploaded via /api/sources/upload) was still
        # rendering the hardcoded "same angle as the main real photo" text, bypassing the agreed form-based
        # camera-angle checklist entirely. Re-seed the static rule text into every stored RuleVersion, and clear
        # the cached vision analysis for classic-path products so they get the new "form" field on next run.
        new_std, new_dirs2 = _read_json("studio_standard.json"), _read_json("shot_directions.json")
        new_schema, new_tpl = _read_json("vision_analysis_schema.json"), (DATA_DIR / "static" / "prompt_template.txt").read_text(encoding="utf-8")
        for rv in s.scalars(select(RuleVersion)):
            d = dict(rv.data or {})
            d["studio_standard"], d["shot_directions"] = new_std, new_dirs2
            d["vision_schema"], d["prompt_template"] = new_schema, new_tpl
            rv.data = d
        from .db import Product as _Product
        for p in s.scalars(select(_Product)):
            if not (p.data or {}).get("lib_key") and p.analysis and "form" not in p.analysis:
                p.analysis = {}
        set_setting(s, "classic_camera_fix_v1", True)
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


# ---------------------------------------------------------------- SkyBlue house layer + unbranded products
UNBRANDED = {"", "unbranded", "generic", "no brand", "nobrand", "china", "made in china", "skyblue select", "none", "-"}


def memory_block(mem: dict, shot_id: str = "") -> str:
    f = mem.get("facts") or {}
    parts = ["[PRODUCT FACTS: reproduce exactly, these override anything else]"]
    for label, key in (("Product", "summary"), ("Exact printed text / logo", "text"), ("Colours", "colours"),
                       ("Shape & proportions", "shape"), ("Parts & counts", "parts"), ("Material & finish", "material"),
                       ("Real size", "size"), ("Must never change", "must_keep"), ("Watch out (past mistakes)", "watch")):
        v = f.get(key)
        if v:
            parts.append(f"- {label}: {', '.join(v) if isinstance(v, list) else v}")
    return "\n".join(parts)

