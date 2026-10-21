"""Google Gemini: vision analysis, image generation (Nano Banana family) and QC review."""
import io
import json
from PIL import Image
from google import genai
from google.genai import types

from ..config import GEMINI_API_KEY, MODEL_VISION, IMAGE_SIZE
from ..secrets import get_secret

QC_CHECKS = ["shape_match", "colour_match", "label_text", "soft_light", "attention_70_30",
             "props_ok", "no_artifacts", "margin_ok"]


def _mime(data: bytes) -> str:
    fmt = (Image.open(io.BytesIO(data)).format or "JPEG").lower()
    return {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(fmt, "image/jpeg")


def _json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`").split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return json.loads(t)


class GeminiProvider:
    def __init__(self):
        key = get_secret("GEMINI_API_KEY") or GEMINI_API_KEY
        if not key:
            raise RuntimeError("Gemini API key missing. Add it in Admin → API & models or set GEMINI_API_KEY.")
        self.client = genai.Client(api_key=key)

    def test(self) -> str:
        names = [m.name for m in self.client.models.list()]
        imgs = [n for n in names if "image" in n]
        return f"{len(names)} models available; image models: {', '.join(n.split('/')[-1] for n in imgs[:6]) or 'none listed'}"

    def analyse(self, raw: bytes, context: dict) -> dict:
        prompt = (
            "You are the product analyst for a product photo studio. Look at the raw product photo and the product data. "
            "Return ONLY JSON matching this schema (use ids from the allowed lists, never invent ids):\n"
            f"{json.dumps(context['schema'])}\n\nAllowed brand_id: {context['brands']}\nAllowed category_id: {context['categories']}\n"
            f"Allowed mood_id: {context['moods']}\nAllowed prop ids per category: {json.dumps(context['props'])}\n"
            f"Product data: {json.dumps(context['product'])}\nSelection rules: {json.dumps(context['rules'])}"
        )
        r = self.client.models.generate_content(
            model=MODEL_VISION,
            contents=[types.Part.from_bytes(data=raw, mime_type=_mime(raw)), prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2))
        return _json(r.text)

    def generate(self, prompt: str, raw: bytes, model: str) -> bytes:
        r = self.client.models.generate_content(
            model=model,
            contents=[types.Part.from_bytes(data=raw, mime_type=_mime(raw)),
                      "Reference image above is the real product. " + prompt],
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio="4:5", image_size=IMAGE_SIZE)))
        for cand in r.candidates or []:
            for part in (cand.content.parts if cand.content else []) or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        reason = getattr(r.candidates[0], "finish_reason", "no image") if r.candidates else "no candidates"
        raise RuntimeError(f"Model returned no image ({reason})")

    def qc(self, raw: bytes, gen: bytes, shot: dict, rules: dict) -> dict:
        prompt = (
            "You are a strict QC reviewer for e-commerce product photos. Image 1 is the real product, image 2 is the generated "
            f"shot ({shot['kind']}). Check each item and return ONLY JSON: "
            '{"checks":{"shape_match":bool,"colour_match":bool,"label_text":bool,"soft_light":bool,"attention_70_30":bool,'
            '"props_ok":bool,"no_artifacts":bool,"margin_ok":bool},"reasons":{"<failed check>":"short reason"}}. '
            "shape_match: same shape, proportions and item count. colour_match: same product colours. label_text: logo and "
            "printed text identical, nothing invented. soft_light: no hard shadows, hot spots or direct-sun look. "
            "attention_70_30: product is the clear focal point. props_ok: at most 5 props, none branded, realistic scale. "
            "no_artifacts: no extra text, watermark, warped hands or geometry. margin_ok: product not cropped, clear margin."
        )
        r = self.client.models.generate_content(
            model=MODEL_VISION,
            contents=[types.Part.from_bytes(data=raw, mime_type=_mime(raw)),
                      types.Part.from_bytes(data=gen, mime_type=_mime(gen)), prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
        data = _json(r.text)
        checks = {k: bool(data.get("checks", {}).get(k, False)) for k in QC_CHECKS}
        return {"checks": checks, "reasons": data.get("reasons", {})}
