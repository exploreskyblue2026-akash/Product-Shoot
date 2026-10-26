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

    def analyse(self, raw: bytes, context: dict) -> dict:
        p = context["product"]
        return {"sku": p.get("code"), "brand_id": p.get("brand_id") or "_default", "category_id": p.get("category_id"),
                "product_description": f"{p.get('name')} (mock analysis)", "visible_text": [p.get("name", "")[:30]],
                "dominant_colours_hex": ["#888888"], "item_count": 1, "risks": []}

    def generate(self, prompt: str, raw: bytes, model: str, size: str = "1K", refs: list | None = None) -> bytes:
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
