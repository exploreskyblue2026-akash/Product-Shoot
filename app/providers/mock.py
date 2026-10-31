"""Offline provider: places the raw product on a coloured 4:5 set so the whole pipeline can be tested without an API."""
import io
import random
from PIL import Image, ImageDraw

from .gemini import QC_CHECKS

SETS = {"hero": (244, 241, 236), "use_case": (228, 244, 253), "detail": (255, 237, 228),
        "arrangement": (253, 232, 241), "lifestyle": (42, 39, 36)}


class MockProvider:
    def test(self) -> str:
        return "Mock provider active (AI_PROVIDER=mock). No real API calls are made."

    def plan_product(self, images, prompt, model=""):
        """Deterministic plan; a photo whose average colour is far from the first one is 'another product'."""
        from PIL import Image, ImageStat
        import io as _io
        means = [ImageStat.Stat(Image.open(_io.BytesIO(b)).convert("RGB").resize((32, 32))).mean for b in images]
        photos = []
        for n, m in enumerate(means):
            far = sum(abs(a - b) for a, b in zip(m, means[0])) > 150
            photos.append({"index": n, "is_this_product": not far, "usable": True, "angle": "front" if n == 0 else "angle",
                           "reason_if_discarded": "different colourway" if far else ""})
        shot = lambda k: {"goal": f"{k} goal", "scene": f"{k} scene", "surface": "light grey", "camera": "3/4, 85mm",  # noqa: E731
                          "composition": "centred, 60%", "props": ["plain pencil"], "hands": "child" if k == "use_case" else "none",
                          "product_state": "closed", "light": "soft", "notes": ""}
        plan = {"photos": photos, "identity": {"type": "pencil case", "form": "box", "parts": ["zip"], "item_count": 1},
                "appearance": {"colours": [{"name": "green", "hex": "#7ED957", "where": "front"}], "materials": ["canvas"],
                               "finish": "matte", "pattern": "doodles", "printed_text": [{"text": "MILAN", "where": "patch"}],
                               "logo": {"text": "MILAN", "form": "patch", "where": "lower centre"}, "hardware": ["2 zips"]},
                "size": {"dimensions": "20 x 13 x 6 cm", "estimated": True, "scale_hint": "hand-held"},
                "use": {"purpose": "school", "user": "kids", "how_used": "on a desk", "hands": "child", "goes_with": ["pencils"]},
                "category": {"name": "stationery", "environment": "kid's study desk", "prop_types": ["pencils"], "mood": "playful"},
                "hero_features": ["print", "patch"], "risks": ["pattern redrawn"], "must_keep": ["MILAN patch", "doodle print"],
                "shots": {k: shot(k) for k in ("hero", "detail", "use_case", "lifestyle")},
                "review_needed": "REVIEWME" in prompt, "review_reason": ""}
        return plan, model or "gemini-3-pro-mock"

    def describe_product(self, ctx):
        return f"<p>{ctx.get('title')}</p><ul><li>By {ctx.get('brand')}</li></ul>"

    def describe_style(self, images):
        return "Soft daylight, colour-block paper backdrops, top-down and 30 degree angles, plain pencils and a small plant."

    def analyse(self, raw: bytes, context: dict) -> dict:
        p = context["product"]
        return {"sku": p.get("code"), "brand_id": p.get("brand_id") or "_default", "category_id": p.get("category_id"),
                "product_description": f"{p.get('name')} (mock analysis)", "visible_text": [p.get("name", "")[:30]],
                "dominant_colours_hex": ["#888888"], "item_count": 1, "risks": []}

    def generate(self, prompt: str, raw: bytes, model: str, size: str = "1K", refs: list | None = None, style: bytes | None = None) -> bytes:
        kind = next((k for k in SETS if f"— {k}" in prompt or f"{k}:" in prompt), "hero")
        bg = Image.new("RGB", (1600, 2000), SETS[kind])
        src = Image.open(io.BytesIO(raw)).convert("RGBA")
        src.thumbnail((1100, 1300))
        bg.paste(src, ((1600 - src.width) // 2, (2000 - src.height) // 2), src)
        ImageDraw.Draw(bg).text((60, 60), f"MOCK · {kind}", fill=(0, 110, 255))
        out = io.BytesIO()
        bg.save(out, "JPEG", quality=88)
        return out.getvalue()

    def qc(self, raw: bytes, gen: bytes, shot: dict, rules: dict) -> dict:
        rnd = random.Random(len(gen) + shot["no"])
        checks = {k: True for k in QC_CHECKS}
        if rnd.random() < 0.12:
            checks["label_text"] = False
            return {"checks": checks, "reasons": {"label_text": "Mock: brand text looks altered"}}
        return {"checks": checks, "reasons": {}}


def _scene(self, prompt: str, model: str, size: str = "1K") -> bytes:
    kind = next((k for k in SETS if f"shot: {k}" in prompt), "hero")
    bg = Image.new("RGB", (1600, 2000), SETS[kind])
    d = ImageDraw.Draw(bg)
    for y in range(1300, 2000, 6):                      # a "surface" so composites have something to sit on
        c = tuple(max(0, v - (y - 1300) // 30) for v in SETS[kind])
        d.line([(0, y), (1600, y)], fill=c, width=6)
    d.ellipse([60, 1500, 300, 1640], fill=(200, 190, 170))   # a prop at the edge
    d.text((60, 60), f"MOCK SCENE · {kind}", fill=(0, 110, 255))
    out = io.BytesIO()
    bg.save(out, "JPEG", quality=88)
    return out.getvalue()


MockProvider.generate_scene = _scene



def _study(self, images: list, ctx: dict) -> dict:
    angles = ["front", "side", "detail", "top"][:len(images)]
    return {"summary": f"{ctx.get('name')} (mock study)", "text": [str(ctx.get("brand") or "")], "colours": ["as photographed"],
            "shape": "as photographed", "parts": [], "material": "", "size": "", "must_keep": ["logo", "colour"],
            "watch": [], "angles": angles, "has_brand_mark": bool(ctx.get("brand"))}


MockProvider.study = _study


def _edit(self, image: bytes, instruction: str, model: str, size: str = "1K", refs=None, style=None) -> bytes:
    """Offline stand-in for an image edit: new background, product untouched (what a good edit should do)."""
    from .. import engine
    im = Image.open(io.BytesIO(image)).convert("RGB")
    mask, _ = engine.product_mask(im)
    bg = Image.new("RGB", im.size, (226, 218, 205))
    d = ImageDraw.Draw(bg)
    for y in range(int(im.height * 0.7), im.height, 4):
        d.line([(0, y), (im.width, y)], fill=(205, 194, 178))
    out = Image.composite(im, bg, mask) if mask is not None else im
    out = out.resize((928, 1160))                       # models answer at their own size
    b = io.BytesIO()
    out.save(b, "JPEG", quality=88)
    return b.getvalue()


MockProvider.edit = _edit
